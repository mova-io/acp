import { useState, useEffect } from 'react'
import { getFileThumbnail, getFilePage, getFileGeometry } from './api.js'
import { PreviewUnavailable } from './PagePreview.jsx'

// A cropped close-up of the bounding box, so the reviewer can inspect the flagged object WITHOUT
// leaving the card (ADR 0018 Slice 3 — zoom-to-object / crop-beside-full). Pure CSS on the page
// render already fetched: the container takes the box's aspect ratio, and the render is used as a
// background scaled so the box fills the frame. No second network call, no new image.
function cropStyle(url, box) {
  const w = Math.min(0.999, Math.max(0.001, box.w))
  const h = Math.min(0.999, Math.max(0.001, box.h))
  return {
    backgroundImage: `url(${url})`,
    backgroundRepeat: 'no-repeat',
    backgroundSize: `${100 / w}%`,               // width scaled so the box width fills the frame (aspect kept)
    backgroundPosition: `${(box.x / (1 - w)) * 100}% ${(box.y / (1 - h)) * 100}%`,
    aspectRatio: `${w} / ${h}`,
  }
}

// A rendered page of a document (ADR 0015). Best-effort: if the backend has no render for this
// file (unsupported type, source unreachable, render failed, SIM mode) it shows an explicit
// "Preview unavailable" placeholder. It used to render nothing, which left "Image 2 of 5" sitting
// above a blank space on the review card — a gap that reads as a layout bug, not as a fact.
//
// Two modes, and they must never be confused:
//   - ORIENTATION (no `page` prop and no `locator`): the document's first page, labelled as a
//     document preview. It is NOT the location of any finding and never says it is.
//   - FINDING PAGE (`page` passed, possibly null, or a `locator`): the page the finding sits on.
//     `page` null with no measured box means no page was recorded → the placeholder says so.
//     There is no page-1 fallback: `page={card.page || 1}` captioned an unplaced finding "Page 1".
//   The page-N render comes from the generic preview route, which CLAMPS an out-of-range page, so
//   the alt text names page N only when the server confirms it drew N (X-ACP-Rendered-Page), or
//   N is 1; otherwise it says the page shown is not confirmed. A server that says it drew another
//   page gets the placeholder, not a picture labelled with the wrong number.
//
// `page` is the page the FINDING sits on (hitl_queue.page), not always the cover. Both the fetch
// and the alt text used to be hardcoded to page 1, so a reviewer judging a finding on page 7 was
// shown page 1 and told, in the alt text, that it was page 1: a picture of the wrong page,
// correctly labelled. Page 1 still takes the cheaper /thumbnail route — the same blob cache the
// certification report warms.
//
// ADR 0018 Slice 2 — when a `locator` (a finding's `part#rId`) is given, the component asks the
// backend for that shape's normalized bounding box and draws a red overlay on the render, so the
// reviewer sees *where* the issue is in <10s. The box carries its OWN page, so we render the page
// the geometry reports (not the `page` prop) to guarantee the box and the picture always agree.
// No box (non-pptx, grouped/inherited transform, SIM, any failure) → the plain large preview at
// the `page` prop, exactly as before. Honesty (ADR 0016): the box is a measured rect or absent.
export default function Thumbnail({ scanId, file, page, locator = null, className = '', maxHeight = 240, kindLabel = null }) {
  const [url, setUrl] = useState(null)
  const [failed, setFailed] = useState(null)       // the reason no image is shown, or null
  const [confirmed, setConfirmed] = useState(false) // did the server confirm the page it drew?
  const [box, setBox] = useState(null)      // {page,x,y,w,h} normalized, or null
  const [geomResolved, setGeomResolved] = useState(false)   // has the geometry fetch settled?
  const [zoom, setZoom] = useState(false)   // Slice 3 — reveal the cropped close-up of the box
  const orientation = page === undefined && !locator
  const recordedPage = Number.isInteger(page) && page > 0 ? page : null
  const fallbackPage = orientation ? 1 : recordedPage

  // Resolve the box first (if a locator is given) — it may override which page we render.
  useEffect(() => {
    setBox(null)
    setGeomResolved(!locator)               // no locator → nothing to wait for; render immediately
    if (!scanId || !file || !locator) return
    let live = true
    getFileGeometry(scanId, file, locator).then((b) => {
      if (live) { setBox(b || null); setGeomResolved(true) }
    })
    return () => { live = false }
  }, [scanId, file, locator])

  const renderPage = box && box.page ? box.page : fallbackPage

  useEffect(() => {
    setUrl(null); setFailed(null); setConfirmed(false)
    if (!scanId || !file) return
    // When a locator is present, wait for the box to resolve before rendering — otherwise we'd
    // render the fallback page, then swap to the box's page (a visible flash of the wrong page).
    if (!geomResolved) return
    if (renderPage == null) { setFailed('no page was recorded for this finding'); return }
    let objectUrl = null
    let live = true
    // Page 1 always exists, so the cheap /thumbnail render IS page 1 and needs no confirmation.
    const png = renderPage === 1
      ? getFileThumbnail(scanId, file).then((blob) => ({ blob, drawn: 1 }))
      : getFilePage(scanId, file, renderPage, { detail: true }).then((got) => (got instanceof Blob
        ? { blob: got, drawn: null }
        : { blob: got?.blob || null, drawn: got?.renderedPage ?? null }))
    png.then(({ blob, drawn }) => {
      if (!live) return
      if (!blob) { setFailed(renderPage === 1 ? 'this document could not be rendered' : `page ${renderPage} could not be rendered`); return }
      if (drawn != null && drawn !== renderPage) { setFailed(`page ${renderPage} is not in the document the preview service holds (it drew page ${drawn})`); return }
      objectUrl = URL.createObjectURL(blob)
      setConfirmed(drawn === renderPage)
      setUrl(objectUrl)
    }).catch(() => { if (live) setFailed('this document could not be rendered') })
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [scanId, file, renderPage, geomResolved])

  if (failed) return <PreviewUnavailable reason={failed} className={`thumb ${className}`.trim()} compact={orientation} />
  if (!url) return null
  const quadrant = box ? quad(box) : null
  const name = file || 'the document'
  const alt = orientation ? `First page of ${name} (document preview — not the location of a finding)`
    : confirmed ? `Page ${renderPage} of ${name}`
      : `Preview of ${name} requested for page ${renderPage}; the page shown is not confirmed`
  return (
    <figure className={`thumb ${box ? 'thumb-boxed' : ''} ${className}`.trim()}>
      <span className="thumb-imgwrap">
        <img src={url} alt={alt} loading="lazy"
             style={{ maxHeight }} />
        {box && (
          <span className="evidence-box" aria-hidden="true"
                style={{ left: `${box.x * 100}%`, top: `${box.y * 100}%`,
                         width: `${box.w * 100}%`, height: `${box.h * 100}%` }} />
        )}
      </span>
      {box && (
        <div className="thumb-tools">
          {quadrant && <span className="thumb-loc">Flagged object · {quadrant}</span>}
          <button type="button" className="thumb-zoom-btn" aria-pressed={zoom}
                  onClick={() => setZoom((z) => !z)}>
            {zoom
              ? (kindLabel ? `Hide ${kindLabel}` : 'Hide close-up')
              : (kindLabel ? `⤢ Zoom to ${kindLabel}` : '⤢ Zoom to object')}
          </button>
        </div>
      )}
      {box && zoom && (
        <figure className="thumb-crop" aria-label={kindLabel ? `Close-up of the flagged ${kindLabel}` : 'Close-up of the flagged object'}>
          <div className="thumb-crop-img" style={cropStyle(url, box)} />
          <figcaption>Close-up · {quadrant}</figcaption>
        </figure>
      )}
      {!box && quadrant && <figcaption className="thumb-loc">Flagged object · {quadrant}</figcaption>}
      {!orientation && !confirmed && (
        <figcaption className="thumb-loc muted"
                    title="The preview service does not say which page it drew, and it substitutes the nearest page when asked for one the document does not have.">
          Page {renderPage} · not confirmed
        </figcaption>
      )}
    </figure>
  )
}

// Derived location string ("top-right") — computed from the real box, never stored or guessed.
function quad(b) {
  const cx = b.x + b.w / 2, cy = b.y + b.h / 2
  const v = cy < 0.4 ? 'top' : cy > 0.6 ? 'bottom' : 'middle'
  const h = cx < 0.35 ? 'left' : cx > 0.65 ? 'right' : 'center'
  return v === 'middle' && h === 'center' ? 'center' : `${v}-${h}`
}

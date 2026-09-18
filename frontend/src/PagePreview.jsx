import { useState, useEffect } from 'react'
import { getFilePage, getExactArtifactPage } from './api.js'

// "Locate in document" evidence — a rendered PNG of a specific page, so a reviewer never has
// to open the document and hunt. When it cannot show that page it says so, with the reason, in a
// small labelled placeholder rather than a broken image or nothing at all.
//
// Two sources, and they differ in what they can promise:
//   - `sha256` given → the EXACT-BYTES route. It renders only bytes with that digest and only a
//     page the document has; anything else is a 404 whose detail ("page 99 is beyond this
//     document's 12 pages") is shown verbatim. What it returns IS that page.
//   - no `sha256` → the generic preview route, which CLAMPS an out-of-range page to the nearest
//     real one. So a picture from it is only captioned as page N when the server says it drew N
//     (X-ACP-Rendered-Page) or N is 1; otherwise the caption says the page is not confirmed. A
//     server that drew a different page is shown as unavailable, never as page N.
//
// No page recorded → no request at all, and the placeholder says so. There is no page-1 default:
// a finding the analyser could not place is not on page 1.
const posInt = (v) => { const n = Number(v); return Number.isInteger(n) && n > 0 ? n : null }

export const PREVIEW_UNAVAILABLE = 'Preview unavailable'

export function PreviewUnavailable({ reason = null, className = '', compact = false }) {
  return (
    <figure className={`pagepreview pagepreview--none ${className}`.trim()} role="note"
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', margin: 0,
                     minWidth: 120, minHeight: compact ? 40 : 90, border: '1px dashed var(--line)',
                     borderRadius: 8, color: 'var(--muted, #6B6670)', fontSize: 11, textAlign: 'center', padding: 8 }}>
      {PREVIEW_UNAVAILABLE}{reason ? ` — ${reason}` : ''}
    </figure>
  )
}

export default function PagePreview({ scanId, file, page = null, sha256 = null, unit = 'page', className = '', maxHeight = 260 }) {
  const want = posInt(page)
  const [state, setState] = useState({ url: null, failed: null, confirmed: false })

  useEffect(() => {
    setState({ url: null, failed: null, confirmed: false })
    if (!scanId || !file || want == null) return
    let objectUrl = null
    let live = true
    const done = (next) => { if (live) setState(next) }
    const show = (blob, confirmed) => {
      objectUrl = URL.createObjectURL(blob)
      done({ url: objectUrl, failed: null, confirmed })
    }
    const request = sha256
      ? getExactArtifactPage(scanId, file, sha256, want).then((got) => {
        if (!got?.ok || !got.blob) {
          done({ url: null, failed: got?.detail || `${unit} ${want} could not be rendered from the recorded version of this document`, confirmed: false })
          return
        }
        if (got.renderedPage != null && got.renderedPage !== want) {
          done({ url: null, failed: `the preview service drew ${unit} ${got.renderedPage}, not ${unit} ${want}`, confirmed: false })
          return
        }
        show(got.blob, true)
      })
      : getFilePage(scanId, file, want, { detail: true }).then((got) => {
        // Tolerates a bare Blob (an older server seam or a test double) — that is "not confirmed".
        const blob = got instanceof Blob ? got : got?.blob
        if (!blob) { done({ url: null, failed: `no image of ${unit} ${want} could be produced`, confirmed: false }); return }
        const drawn = got instanceof Blob ? null : posInt(got?.renderedPage)
        if (drawn != null && drawn !== want) {
          done({ url: null, failed: `${unit} ${want} is not in the document the preview service holds (it drew ${unit} ${drawn})`, confirmed: false })
          return
        }
        show(blob, drawn === want || want === 1)
      })
    request.catch(() => done({ url: null, failed: `no image of ${unit} ${want} could be produced`, confirmed: false }))
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [scanId, file, want, sha256, unit])

  const Unit = unit === 'slide' ? 'Slide' : 'Page'
  if (want == null) return <PreviewUnavailable reason={`no ${unit} was recorded for this finding`} className={className} />
  if (state.failed) return <PreviewUnavailable reason={state.failed} className={className} />
  if (!state.url) return null
  return (
    <figure className={`pagepreview ${className}`.trim()} style={{ margin: 0 }}>
      <img src={state.url}
           alt={state.confirmed ? `${file || 'document'} — ${unit} ${want}` : `${file || 'document'} — preview requested for ${unit} ${want}; the ${unit} shown is not confirmed`}
           loading="lazy"
           style={{ maxHeight, maxWidth: '100%', borderRadius: 6, border: '1px solid var(--line)', display: 'block' }} />
      <figcaption className="muted" style={{ fontSize: 11, marginTop: 3, textAlign: 'center' }}
                  title={state.confirmed ? undefined : `The preview service does not say which ${unit} it drew, and it substitutes the nearest ${unit} when asked for one the document does not have.`}>
        {Unit} {want}{state.confirmed ? '' : ' · not confirmed'}
      </figcaption>
    </figure>
  )
}

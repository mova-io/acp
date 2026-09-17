import { useState, useEffect, useRef } from 'react'
import { getConfig } from './api'
import './version-toast.css'

const POLL_MS = 10 * 60 * 1000  // 10 minutes

// WCAG 1.4.3: the September 2026 self-assessment reported this banner's text contrast as
// insufficient. By our own calculation the old #16a34a behind white 14px/500 text is 3.30:1;
// #15803d gives 5.02:1 for the message, the "Reload now" label (#15803d on
// #fff) and the dismiss glyph (now solid white; it was 80% white = 2.63:1 on the old green).
// versionToast.test.jsx computes these ratios from the rendered styles.
const TOAST_BG = '#15803d'
const TOAST_FG = '#ffffff'

// Full-width top banner that appears when the server version advances past the version the page
// loaded with. Polls /config (public, pre-auth endpoint) on an interval so it works across
// long sessions. Uses baseRef so the poll closure never captures a stale version string.

// Narrow screens (WCAG 1.4.10 at 320 CSS px, 1.4.4 at 200% text). The dismiss ✕ is absolutely
// positioned, so flex layout does not know it is there. With the old 16px side padding the
// centred message + "Reload now" (~360px wide at 14px) filled the whole 288px content box at
// 320px and ran under the ✕. It did so at any viewport below ~440px. The banner now reserves a
// column on each side for the ✕ (right offset + target + focus ring + breathing room), keeping
// the content centred, and lets the row wrap so the button drops below the message.
export const DISMISS_RIGHT = 14
export const DISMISS_SIZE = 28          // ≥ 24×24 CSS px target (WCAG 2.5.8)
export const FOCUS_RING = 4             // outline 2px + outline-offset 2px (version-toast.css)
export const SIDE_RESERVE = DISMISS_RIGHT + DISMISS_SIZE + FOCUS_RING + 6   // 52px

export function VersionToastBanner({ onReload, onDismiss }) {
  return (
    <div role="status" aria-live="polite" className="acp-version-toast"
         style={{
           position: 'fixed', top: 0, left: 0, right: 0,
           background: TOAST_BG,
           color: TOAST_FG,
           padding: `10px ${SIDE_RESERVE}px`,
           display: 'flex', flexWrap: 'wrap',
           alignItems: 'center', justifyContent: 'center', gap: '8px 14px',
           boxShadow: '0 2px 8px rgba(0,0,0,.18)',
           zIndex: 9999,
           fontSize: 14,
           fontWeight: 500,
         }}>
      <span style={{ minWidth: 0, overflowWrap: 'anywhere', textAlign: 'center' }}>
        <span aria-hidden="true">✦ </span>A new version of ACP is available.
      </span>
      <button type="button"
              className="acp-version-toast__control"
              onClick={onReload}
              style={{
                background: TOAST_FG, color: TOAST_BG, border: 'none',
                borderRadius: 6, padding: '4px 14px', fontWeight: 650,
                fontSize: 13, cursor: 'pointer', minHeight: 24,
              }}>
        Reload now
      </button>
      <button type="button" onClick={onDismiss}
              className="acp-version-toast__control"
              aria-label="Dismiss version notification"
              style={{
                position: 'absolute', right: DISMISS_RIGHT, top: '50%', transform: 'translateY(-50%)',
                width: DISMISS_SIZE, height: DISMISS_SIZE,
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                background: 'transparent', border: 'none', color: TOAST_FG,
                fontSize: 18, cursor: 'pointer', lineHeight: 1, padding: 0,
              }}>✕</button>
    </div>
  )
}

export default function VersionToast({ currentVersion }) {
  const [show, setShow] = useState(false)
  const baseRef = useRef(currentVersion)

  useEffect(() => {
    // Don't start polling until we know what version the page loaded with.
    if (!baseRef.current) return
    const t = setInterval(() => {
      getConfig().then((c) => {
        if (c?.version && c.version !== baseRef.current) setShow(true)
      }).catch(() => {})
    }, POLL_MS)
    return () => clearInterval(t)
  }, [])

  if (!show) return null
  return (
    <VersionToastBanner
      onReload={() => window.location.reload()}
      onDismiss={() => setShow(false)}
    />
  )
}

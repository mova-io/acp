// One clearly labeled report menu: three modes ("Summary", "Reviewer packet", "Full evidence"),
// each offered in the formats the caller supplies (PDF and/or HTML).
//
// `formats` = [{ key: 'pdf'|'html', label: 'PDF', run: (mode) => Promise }]. A `run` may resolve
// `{ ok: false, message }` (the server renderer's refusal) or throw; either way the failure is
// shown on screen in an alert, never only logged. While one export runs the others are disabled
// and a live region says what is being generated.
import { useState, useRef } from 'react'
import { REPORT_MODES } from './reportRenderClient.js'

// Contract labels come from the renderer client (one definition); order is the menu order.
export const REPORT_MODE_OPTIONS = Object.freeze([
  { mode: 'summary', label: REPORT_MODES.summary, hint: 'One-page decision summary' },
  { mode: 'reviewer', label: REPORT_MODES.reviewer, hint: 'Summary plus changes to confirm and remaining work' },
  { mode: 'full', label: REPORT_MODES.full, hint: 'Everything, with the complete evidence appendix' },
])

// `inline` renders the choices as a labeled group (for use inside an existing Reports menu)
// instead of its own disclosure.
export default function ReportModeMenu({ label = 'Report', formats = [], disabled = false, disabledReason = null, className = 'reports-menu', inline = false }) {
  const [busy, setBusy] = useState(null)      // { mode, format }
  const [status, setStatus] = useState(null)  // { ok, text }
  const running = useRef(false)

  const start = async (mode, fmt) => {
    if (running.current || disabled) return
    running.current = true
    const modeLabel = REPORT_MODE_OPTIONS.find((m) => m.mode === mode)?.label || mode
    setBusy({ mode, format: fmt.key }); setStatus(null)
    try {
      const res = await fmt.run(mode)
      if (res && res.ok === false && res.fallback === 'html') {
        // The PDF was not produced, but an HTML copy of the same report WAS downloaded. That is
        // neither a success for the format asked for nor a failure to deliver the report.
        setStatus({ ok: true, text: `${modeLabel} (${fmt.label}) was not generated; an HTML copy was downloaded instead. ${res.message || ''}`.trim() })
      } else if (res && res.ok === false) {
        setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was not generated: ${res.message || 'the report could not be produced.'}` })
      } else {
        setStatus({ ok: true, text: `${modeLabel} (${fmt.label}) generated.` })
      }
    } catch (e) {
      setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was not generated: ${e?.message || 'unexpected error'}` })
    } finally {
      running.current = false
      setBusy(null)
    }
  }

  const busyLabel = busy && `${REPORT_MODE_OPTIONS.find((m) => m.mode === busy.mode)?.label} (${formats.find((f) => f.key === busy.format)?.label})`
  const choices = (
    <>
      {REPORT_MODE_OPTIONS.map(({ mode, label: ml, hint }) => (
        <div key={mode} className="reportmode-row" role="group" aria-label={ml} style={{ padding: '4px 0' }}>
          <div style={{ fontWeight: 600, fontSize: 12.5 }}>{ml}</div>
          <div className="muted" style={{ fontSize: 11 }}>{hint}</div>
          <div className="reportmode-formats" style={{ display: 'flex', gap: 4 }}>
            {formats.map((f) => (
              <button key={f.key} type="button" className="ghost small"
                      aria-label={`${ml} — ${f.label}`}
                      disabled={disabled || !!busy}
                      onClick={() => start(mode, f)}>
                {f.label}
              </button>
            ))}
          </div>
        </div>
      ))}
      {disabled && disabledReason && <p className="muted" style={{ fontSize: 11 }}>{disabledReason}</p>}
    </>
  )
  return (
    <div className="reportmode" style={{ display: inline ? 'block' : 'inline-block' }}>
      {inline ? (
        <div role="group" aria-label={label} className="reportmode-inline" style={{ padding: '4px 8px' }}>
          <div style={{ fontWeight: 700, fontSize: 12.5 }}>{busy ? `Generating ${busyLabel}…` : label}</div>
          {choices}
        </div>
      ) : (
        <details className={className}>
          <summary className="exportbtn" aria-disabled={disabled || undefined} title={disabled ? disabledReason || undefined : undefined}>
            {busy ? `Generating ${busyLabel}…` : label}
          </summary>
          <div className="reports-menu-items" role="group" aria-label={`${label} — choose a report and format`}>
            {choices}
          </div>
        </details>
      )}
      <span role="status" aria-live="polite" className="muted" style={{ fontSize: 12, marginLeft: 6 }}>
        {busy ? `Generating ${busyLabel}…` : status?.ok ? status.text : ''}
      </span>
      {status && !status.ok && (
        <p role="alert" style={{ color: 'var(--error-fg)', fontSize: 12, margin: '4px 0 0' }}>{status.text}</p>
      )}
    </div>
  )
}

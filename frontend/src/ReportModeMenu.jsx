// One clearly labeled report menu: three modes ("Summary", "Reviewer packet", "Full evidence"),
// each offered in the formats the caller supplies (PDF, HTML, and for scans "Per-file packets
// (ZIP)").
//
// The three modes are three DIFFERENT documents for three different readers — not three lengths
// of one document — and each says so in its row:
//   Summary          a decision-maker: one page, where it stands and what is left, nothing itemised;
//   Reviewer packet  the person confirming the work: every change to confirm and every remaining
//                    action, with long lists bounded and the omissions named;
//   Full evidence    an auditor: every recorded finding, change and decision, uncapped.
//
// `formats` = [{ key, label, run: (mode, ctx) => Promise, cancellable?, modes?, progressText? }].
//   - A `run` may resolve `{ ok: false, message }` (a refusal or an incomplete export) or throw;
//     either way the failure is shown on screen in an alert, never only logged.
//   - `cancellable` formats receive `ctx = { signal, onProgress }`; while they run a Cancel button
//     is offered, and `progressText(progress)` is announced in the live region.
//   - `modes` limits a format to some modes (default: all three).
//   - A result may carry `downloads: [{ label, blob, filename }]` (e.g. a master index on its own);
//     they are offered as buttons after the run.
// While one export runs the others are disabled and a live region says what is being generated.
import { useState, useRef } from 'react'
import { REPORT_MODES, downloadBlob } from './reportRenderClient.js'

// Contract labels come from the renderer client (one definition); order is the menu order.
export const REPORT_MODE_OPTIONS = Object.freeze([
  { mode: 'summary', label: REPORT_MODES.summary, hint: 'For a decision: one page on where it stands and what is left. Nothing is itemised.' },
  { mode: 'reviewer', label: REPORT_MODES.reviewer, hint: 'For the person confirming the work: each change to confirm and each remaining action. Long lists are capped, and the report says what it leaves out.' },
  { mode: 'full', label: REPORT_MODES.full, hint: 'For audit: every recorded finding, change and decision, with no caps.' },
])

// `inline` renders the choices as a labeled group (for use inside an existing Reports menu)
// instead of its own disclosure. `modeNotes` = { [mode]: string } adds a caller-specific note
// under a mode (e.g. steering a large scan's Full evidence to per-file packets).
export default function ReportModeMenu({ label = 'Report', formats = [], disabled = false, disabledReason = null, className = 'reports-menu', inline = false, progress = null, modeNotes = null }) {
  const [busy, setBusy] = useState(null)      // { mode, format }
  const [status, setStatus] = useState(null)  // { ok, text, downloads? }
  const [runProgress, setRunProgress] = useState(null)
  const running = useRef(false)
  const abortRef = useRef(null)

  const start = async (mode, fmt) => {
    if (running.current || disabled) return
    running.current = true
    const modeLabel = REPORT_MODE_OPTIONS.find((m) => m.mode === mode)?.label || mode
    setBusy({ mode, format: fmt.key }); setStatus(null); setRunProgress(null)
    const ac = fmt.cancellable ? new AbortController() : null
    abortRef.current = ac
    try {
      const res = await (ac ? fmt.run(mode, { signal: ac.signal, onProgress: setRunProgress }) : fmt.run(mode))
      const downloads = Array.isArray(res?.downloads) ? res.downloads : null
      if (res && res.cancelled) {
        setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was cancelled. ${res.message || ''}`.trim(), downloads })
      } else if (res && res.incomplete) {
        // Something WAS downloaded, but it does not cover everything it was asked to. Neither
        // "generated" nor "not generated" is true; say what it is.
        setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was downloaded but is INCOMPLETE. ${res.message || ''}`.trim(), downloads })
      } else if (res && res.ok === false && res.fallback === 'html') {
        // The PDF was not produced, but an HTML copy of the same report WAS downloaded. That is
        // neither a success for the format asked for nor a failure to deliver the report. It is
        // reported as a FAILURE of the format that was asked for — a live region saying "generated"
        // when no PDF exists is how a menu comes to claim a PDF it never produced.
        setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was NOT generated. An HTML copy of the same report was downloaded instead. ${res.message || ''}`.trim() })
      } else if (res && res.regenerate) {
        // The server refused to stamp its identity on evidence that has moved on. This is an
        // instruction, not an error to log — say the action.
        setStatus({ ok: false, regenerate: true, text: `${modeLabel} (${fmt.label}) was not generated: ${res.message || 'the report data is out of date.'}` })
      } else if (res && res.ok === false) {
        setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was not generated: ${res.message || 'the report could not be produced.'}`, downloads })
      } else {
        setStatus({ ok: true, text: `${modeLabel} (${fmt.label}) generated.${res?.message ? ` ${res.message}` : ''}`, downloads })
      }
    } catch (e) {
      setStatus({ ok: false, text: `${modeLabel} (${fmt.label}) was not generated: ${e?.message || 'unexpected error'}` })
    } finally {
      running.current = false
      abortRef.current = null
      setBusy(null)
      setRunProgress(null)
    }
  }

  const busyFmt = busy && formats.find((f) => f.key === busy.format)
  const busyLabel = busy && `${REPORT_MODE_OPTIONS.find((m) => m.mode === busy.mode)?.label} (${busyFmt?.label})`
  const liveText = busy
    ? ((runProgress && busyFmt?.progressText?.(runProgress)) || progress || `Generating ${busyLabel}…`)
    : status?.ok ? status.text : ''
  const choices = (
    <>
      {REPORT_MODE_OPTIONS.map(({ mode, label: ml, hint }) => {
        const offered = formats.filter((f) => !Array.isArray(f.modes) || f.modes.includes(mode))
        return (
          <div key={mode} className="reportmode-row" role="group" aria-label={ml} style={{ padding: '4px 0' }}>
            <div style={{ fontWeight: 600, fontSize: 12.5 }}>{ml}</div>
            <div className="muted" style={{ fontSize: 11 }}>{hint}</div>
            {modeNotes?.[mode] && <div className="muted reportmode-note" style={{ fontSize: 11, fontStyle: 'italic' }}>{modeNotes[mode]}</div>}
            <div className="reportmode-formats" style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
              {offered.map((f) => (
                <button key={f.key} type="button" className="ghost small"
                        aria-label={`${ml} — ${f.label}`}
                        title={f.title || undefined}
                        disabled={disabled || !!busy}
                        onClick={() => start(mode, f)}>
                  {f.label}
                </button>
              ))}
            </div>
          </div>
        )
      })}
      {disabled && disabledReason && <p className="muted" style={{ fontSize: 11 }}>{disabledReason}</p>}
    </>
  )
  const cancelBtn = busy && busyFmt?.cancellable && (
    <button type="button" className="ghost small reportmode-cancel" style={{ marginLeft: 6 }}
            onClick={() => abortRef.current?.abort()}>
      Cancel {busyFmt.label}
    </button>
  )
  const extraDownloads = !busy && status?.downloads?.length > 0 && (
    <div className="reportmode-downloads" role="group" aria-label="Download separately" style={{ display: 'flex', gap: 4, marginTop: 4, flexWrap: 'wrap' }}>
      {status.downloads.map((d) => (
        <button key={d.filename} type="button" className="ghost small" onClick={() => downloadBlob(d.blob, d.filename)}>
          Download {d.label}
        </button>
      ))}
    </div>
  )
  return (
    <div className="reportmode" style={{ display: inline ? 'block' : 'inline-block' }}>
      {inline ? (
        <div role="group" aria-label={label} className="reportmode-inline" style={{ padding: '4px 8px' }}>
          <div style={{ fontWeight: 700, fontSize: 12.5 }}>{busy ? liveText : label}</div>
          {choices}
        </div>
      ) : (
        <details className={className}>
          <summary className="exportbtn" aria-disabled={disabled || undefined} title={disabled ? disabledReason || undefined : undefined}>
            {busy ? liveText : label}
          </summary>
          <div className="reports-menu-items" role="group" aria-label={`${label} — choose a report and format`}>
            {choices}
          </div>
        </details>
      )}
      <span role="status" aria-live="polite" className="muted" style={{ fontSize: 12, marginLeft: 6 }}>
        {liveText}
      </span>
      {cancelBtn}
      {status && !status.ok && (
        <p role="alert" className={status.regenerate ? 'reportmode-regenerate' : undefined}
           style={{ color: 'var(--error-fg)', fontSize: 12, margin: '4px 0 0' }}>{status.text}</p>
      )}
      {extraDownloads}
    </div>
  )
}

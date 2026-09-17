import { docProgress, workflowStatusOf } from './remediationInboxModel.js'

// The persistent progress bar above the three-pane remediation workspace. It answers "how close is
// THIS document to done?" at a glance — resolved/total actions, a percent, and remaining action counts —
// so a reviewer working a long file always sees the finish line without counting rows in the queue.
//
// Per-document by design (it reports the SELECTED finding's file); before anything is selected it
// falls back to the whole run so the bar is never empty. Pure math lives in remediationInboxModel's
// docProgress — this file is presentation.

export default function WorkspaceProgress({ queue = [], decisions = {}, selected = null }) {
  if (!queue.length) return null
  const file = selected?.file || null
  const p = docProgress(queue, file, decisions)
  const items = file ? queue.filter(row => row.file === file) : queue
  const completed = items.filter(row => workflowStatusOf(row, decisions) === 'completed').length
  const processing = items.filter(row => workflowStatusOf(row, decisions) === 'awaiting-validation').length
  const done = completed === p.total
  const pct = Math.round(completed / p.total * 100)
  const title = file || 'This remediation run'
  const barColor = done ? '#1f9d6b' : '#2f6fed'

  return (
    <div className="rem-wsprog" style={{
      padding: '10px 14px', border: '1px solid var(--line,#e2dce4)', borderRadius: 12,
      background: 'var(--surface-2,#f6f5f8)', marginBottom: 10,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 700, fontSize: 13.5, minWidth: 0, maxWidth: '55%',
                       whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={title}>
          {title}
        </span>
        <span className="muted" style={{ fontSize: 12.5 }}>
          {completed} of {p.total} action{p.total === 1 ? '' : 's'} complete
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 12.5, fontWeight: 600, color: done ? '#1f9d6b' : 'var(--ink)' }}>
          {done ? 'Complete ✓' : processing ? `${processing} applying / awaiting verification` : `${p.total - completed} actions awaiting an outcome`}
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <div role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
             aria-label={`${title}: ${pct}% complete`}
             style={{ flex: '1 1 auto', height: 8, borderRadius: 6, background: 'var(--line,#e2dce4)', overflow: 'hidden' }}>
          <div style={{ width: `${pct}%`, height: '100%', background: barColor, transition: 'width .25s ease' }} />
        </div>
        <span style={{ flex: '0 0 auto', fontSize: 12.5, fontWeight: 700, fontVariantNumeric: 'tabular-nums', minWidth: 34, textAlign: 'right' }}>
          {pct}%
        </span>
      </div>
    </div>
  )
}

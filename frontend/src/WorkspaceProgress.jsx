import { reviewProgressOf } from './remediationInboxModel.js'
import { progressLabelsOf } from './reviewPopulationExplanation.js'

// The persistent progress bar above the three-pane remediation workspace. It answers "how close is
// THIS document to done?" at a glance — tasks with a final outcome out of all tasks, a percent, and
// what is still awaiting an outcome — so a reviewer working a long file always sees the finish line.
//
// Per-document by design (it reports the SELECTED finding's file); before anything is selected it
// falls back to the whole run so the bar is never empty.
//
// ONE DENOMINATOR, ONE DEFINITION. This bar used to say "3 of 5 actions complete / 2 awaiting
// outcome" two lines below a header saying "4 of 5 reviewed" — the same five rows, two meanings of
// done, no way to reconcile them by reading. Both now come from reviewProgressOf and are worded by
// progressLabelsOf, and the definition is printed under the bar rather than left to the docs.

export default function WorkspaceProgress({ queue = [], decisions = {}, selected = null }) {
  if (!queue.length) return null
  const file = selected?.file || null
  const items = file ? queue.filter(row => row.file === file) : queue
  const p = progressLabelsOf(reviewProgressOf(items, decisions))
  if (!p.total) return null
  const done = p.finished === p.total
  const pct = Math.round(p.finished / p.total * 100)
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
        <span className="muted" style={{ fontSize: 12.5 }}>{p.finishedLabel}</span>
        <span style={{ marginLeft: 'auto', fontSize: 12.5, fontWeight: 600, color: done ? '#1f9d6b' : 'var(--ink)' }}>
          {done ? 'Complete ✓' : p.decidedLabel}
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <div role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
             aria-label={`${title}: ${p.finished} of ${p.total} tasks have a final outcome`}
             style={{ flex: '1 1 auto', height: 8, borderRadius: 6, background: 'var(--line,#e2dce4)', overflow: 'hidden' }}>
          <div style={{ width: `${pct}%`, height: '100%', background: barColor, transition: 'width .25s ease' }} />
        </div>
        <span style={{ flex: '0 0 auto', fontSize: 12.5, fontWeight: 700, fontVariantNumeric: 'tabular-nums', minWidth: 34, textAlign: 'right' }}>
          {pct}%
        </span>
      </div>
      <p className="muted rem-wsprog__definition" style={{ margin: '6px 0 0', fontSize: 11.5 }}>{p.definition}</p>
    </div>
  )
}

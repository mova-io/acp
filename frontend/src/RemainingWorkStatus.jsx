import { remainingWorkStatus } from './remainingWorkStatus.js'
import './remaining-work-status.css'

export default function RemainingWorkStatus(props) {
  const { summary, notices, checkpoint, recovery, stalled, humanTotal, statusTotal } = remainingWorkStatus(props)
  if (!notices.length && !checkpoint && !recovery) return null
  const routine = notices.filter(notice => notice.presentation === 'detail')
  const actionable = notices.filter(notice => notice.presentation !== 'detail')
  const repeatedStatus = routine.filter(notice => notice.population === 'status'
    && notice.label === 'Recorded status needs checking')
  const routineRows = routine.filter(notice => !repeatedStatus.includes(notice))
  return <section className="remaining-work-status" aria-label="Remaining work and recovery">
    <h3>What happens next</h3>
    {(checkpoint || recovery) && <p className={stalled ? 'remaining-work-stalled' : 'muted'} role={stalled ? 'alert' : undefined}>{checkpoint} {recovery}</p>}
    {(humanTotal > 0 || statusTotal > 0) && <p aria-label="Remaining work item counts"><b>{humanTotal.toLocaleString()} need your input</b> · {statusTotal.toLocaleString()} status checks. These are separate item groups; document notices below are not added to either total.</p>}
    {summary.total > 0 && <section aria-label="Remaining review items by next step">
      <h4>Remaining review items by next step</h4>
      <p>{summary.total.toLocaleString()} review items in this view. Each item appears once below; these are not document or unresolved-finding totals.</p>
      <ul>{summary.groups.filter(group => group.count > 0).map(group => <li key={group.key} className="remaining-work-waiting">
        <strong>{group.label} · {group.count.toLocaleString()} review item{group.count === 1 ? '' : 's'}</strong>
        <span>{group.nextStep}</span>
      </li>)}</ul>
    </section>}
    {actionable.length > 0 && <><p className="muted">Items below need a decision or a recovery action. They are not additional finding totals.</p><ul>{actionable.map(notice => <li key={notice.key} className={`remaining-work-${notice.tone}`}><strong>{notice.population === 'status' && <small>Status checks · </small>}{notice.label}</strong><span>{notice.responsibility}</span></li>)}</ul></>}
    {routine.length > 0 && <details className="remaining-work-details">
      <summary>Status and recovery details · {routine.length.toLocaleString()} recorded group{routine.length === 1 ? '' : 's'}</summary>
      <p className="muted">Queued retries, preparation waits, and unclassified saved statuses stay available here. A recorded status is not a verified fix.</p>
      <ul>
        {repeatedStatus.length > 0 && <li className="remaining-work-waiting remaining-work-status-groups">
          <strong>Recorded status needs checking</strong>
          <details><summary>{repeatedStatus.length.toLocaleString()} separate recorded group{repeatedStatus.length === 1 ? '' : 's'}</summary>
            <ul>{repeatedStatus.map(notice => <li key={notice.key}><span>{notice.responsibility}</span></li>)}</ul>
          </details>
        </li>}
        {routineRows.map(notice => <li key={notice.key} className={`remaining-work-${notice.tone}`}><strong>{notice.population === 'status' && <small>Status checks · </small>}{notice.label}</strong><span>{notice.responsibility}</span></li>)}
      </ul>
    </details>}
  </section>
}

import { releaseOutcomeSummary, savedDestinationLinks } from './releaseOutcomeSummary.js'
import './remediation-assessment-progress.css'
import './release-outcome-summary.css'

const number = value => value === null ? 'Unavailable' : value.toLocaleString()
export default function ReleaseOutcomeSummary({ folders = [], onRetry, ...evidence }) {
  const summary = releaseOutcomeSummary(evidence)
  const links = savedDestinationLinks(folders)
  return <section className={`release-outcome-summary release-outcome-summary--${summary.state}`} aria-label="Release outcome summary">
    <div className="wf-section-head"><h3>{summary.title}</h3><span>Recorded results</span></div>
    <dl className="rap-grid">
      <div className={`rap-tile${summary.fixed !== null ? ' rap-green' : ''}`}><dt>Fixed and verified · findings</dt><dd>{number(summary.fixed)}<p>Original findings with recorded verification evidence for this scope.</p></dd></div>
      <div className="rap-tile"><dt>Findings still open</dt><dd>{number(summary.open)}<p>Assessed findings without a recorded verified fix, including exclusions and replaced findings.</p></dd></div>
      <div className={`rap-tile${summary.complete ? ' rap-green' : ''}`}><dt>Copies delivered</dt><dd>{number(summary.delivered)}{summary.total !== null && <small> of {number(summary.total)} authorized copies</small>}<p>Current corrected copies confirmed at the saved destination.</p></dd></div>
    </dl>
    <p role="status">{summary.complete ? 'All authorized copies have confirmed delivery. Remaining findings stay in the follow-up checklist.'
      : summary.remainingCopies !== null ? `${number(summary.remainingCopies)} authorized ${summary.remainingCopies === 1 ? 'copy still awaits' : 'copies still await'} confirmed delivery. ${summary.state === 'attention' ? 'Inspect the saved-plan recovery status below.' : 'Covered copies continue automatically under your Q3 approval.'}`
        : summary.deliveryReason || 'The full saved scope and current-copy receipts must agree before delivery can be marked complete.'}</p>
    {summary.deliveryReason && onRetry && <button type="button" className="linklike" onClick={onRetry}>Refresh release evidence</button>}
    {summary.excluded !== null && (summary.excluded > 0 || summary.superseded > 0) && <p>{number(summary.excluded)} excluded by plan · {number(summary.superseded)} replaced by reassessment. These are included in findings still open and remain separate from verified fixes.</p>}
    <p className="muted">Delivery does not certify accessibility. Findings and document copies are separate counts.</p>
    {!!links.length && <nav aria-label="Published destinations">{links.map(link => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">Open {link.name} ↗</a>)}</nav>}
  </section>
}
